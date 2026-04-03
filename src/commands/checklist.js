const { SlashCommandBuilder } = require("discord.js");
const { getGuildData, saveStore } = require("../utils/store");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");

function renderChecklist(name, checklist) {
  const items = checklist.items
    .map((item, index) => `${item.done ? "[x]" : "[ ]"} ${index + 1}. ${item.text}`)
    .join("\n");
  return `**${name}**\n${items || "No items yet."}`;
}

module.exports = {
  data: new SlashCommandBuilder()
    .setName("checklist")
    .setDescription("Manage team checklists.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("create")
        .setDescription("Create a checklist from pipe-delimited items.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Checklist name.").setRequired(true)
        )
        .addStringOption((option) =>
          option
            .setName("items")
            .setDescription("Items separated with | characters.")
            .setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("add-item")
        .setDescription("Add an item to a checklist.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Checklist name.").setRequired(true)
        )
        .addStringOption((option) =>
          option.setName("item").setDescription("New item text.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("done")
        .setDescription("Mark one checklist item as done.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Checklist name.").setRequired(true)
        )
        .addIntegerOption((option) =>
          option.setName("index").setDescription("Item number starting at 1.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("reset")
        .setDescription("Reset every item in a checklist.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Checklist name.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("show")
        .setDescription("Show a checklist.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Checklist name.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List checklist names."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("delete")
        .setDescription("Delete a checklist.")
        .addStringOption((option) =>
          option.setName("name").setDescription("Checklist name.").setRequired(true)
        )),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "list") {
      const names = Object.keys(guildData.checklists).sort();
      await interaction.reply({
        content: names.length ? names.map((name) => `• ${name}`).join("\n") : "No checklists saved yet.",
        ephemeral: true
      });
      return;
    }

    if (subcommand === "show") {
      const name = interaction.options.getString("name", true).toLowerCase().trim();
      const checklist = guildData.checklists[name];
      await interaction.reply({
        content: checklist ? renderChecklist(name, checklist) : `Checklist \`${name}\` does not exist.`,
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/checklist`"
    );
    if (!hasPermission) {
      return;
    }

    const name = interaction.options.getString("name", true).toLowerCase().trim();

    if (subcommand === "create") {
      const items = interaction.options
        .getString("items", true)
        .split("|")
        .map((item) => item.trim())
        .filter(Boolean)
        .map((text) => ({ text, done: false }));

      guildData.checklists[name] = {
        items,
        createdBy: interaction.user.id,
        createdAt: Date.now()
      };
      saveStore();

      await interaction.reply({
        content: `Checklist \`${name}\` created with ${items.length} item(s).`,
        ephemeral: true
      });
      return;
    }

    const checklist = guildData.checklists[name];
    if (!checklist) {
      await interaction.reply({
        content: `Checklist \`${name}\` does not exist.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "add-item") {
      checklist.items.push({
        text: interaction.options.getString("item", true),
        done: false
      });
      saveStore();
      await interaction.reply({
        content: `Added a new item to \`${name}\`.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "done") {
      const index = interaction.options.getInteger("index", true) - 1;
      if (!checklist.items[index]) {
        await interaction.reply({
          content: "That checklist item number does not exist.",
          ephemeral: true
        });
        return;
      }

      checklist.items[index].done = true;
      saveStore();
      await interaction.reply({
        content: `Marked item ${index + 1} in \`${name}\` as done.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "reset") {
      for (const item of checklist.items) {
        item.done = false;
      }

      saveStore();
      await interaction.reply({
        content: `Checklist \`${name}\` reset.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "delete") {
      delete guildData.checklists[name];
      saveStore();
      await interaction.reply({
        content: `Checklist \`${name}\` deleted.`,
        ephemeral: true
      });
    }
  }
};
