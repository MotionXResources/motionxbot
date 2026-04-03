const { SlashCommandBuilder } = require("discord.js");
const { randomUUID } = require("node:crypto");
const { getGuildData, saveStore } = require("../utils/store");
const { ensureMemberPermissions, PermissionFlagsBits } = require("../utils/discord");
const { parseDuration, renderDiscordTimestamp } = require("../utils/time");

module.exports = {
  data: new SlashCommandBuilder()
    .setName("todo")
    .setDescription("Manage shared operational todos.")
    .addSubcommand((subcommand) =>
      subcommand
        .setName("add")
        .setDescription("Add a todo.")
        .addStringOption((option) =>
          option.setName("title").setDescription("Todo title.").setRequired(true)
        )
        .addUserOption((option) =>
          option.setName("assignee").setDescription("Optional assignee.")
        )
        .addStringOption((option) =>
          option.setName("due-in").setDescription("Optional due delay like 3d.")
        ))
    .addSubcommand((subcommand) =>
      subcommand.setName("list").setDescription("List todos."))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("done")
        .setDescription("Mark a todo as done.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Todo id.").setRequired(true)
        ))
    .addSubcommand((subcommand) =>
      subcommand
        .setName("remove")
        .setDescription("Remove a todo.")
        .addStringOption((option) =>
          option.setName("id").setDescription("Todo id.").setRequired(true)
        )),
  async execute(interaction) {
    const subcommand = interaction.options.getSubcommand();
    const guildData = getGuildData(interaction.guildId);

    if (subcommand === "list") {
      if (!guildData.todos.length) {
        await interaction.reply({
          content: "No todos saved yet.",
          ephemeral: true
        });
        return;
      }

      const lines = guildData.todos
        .sort((left, right) => {
          if (left.done !== right.done) return Number(left.done) - Number(right.done);
          return (left.dueAt || Number.MAX_SAFE_INTEGER) - (right.dueAt || Number.MAX_SAFE_INTEGER);
        })
        .map((todo) => {
          const assignee = todo.assigneeId ? `<@${todo.assigneeId}>` : "unassigned";
          const due = todo.dueAt ? renderDiscordTimestamp(todo.dueAt) : "no due date";
          return `\`${todo.id}\` • ${todo.done ? "done" : "open"} • ${assignee} • ${due} • ${todo.title}`;
        });

      await interaction.reply({
        content: lines.join("\n"),
        ephemeral: true
      });
      return;
    }

    const hasPermission = await ensureMemberPermissions(
      interaction,
      PermissionFlagsBits.ManageGuild,
      "`/todo`"
    );
    if (!hasPermission) {
      return;
    }

    if (subcommand === "add") {
      const dueIn = interaction.options.getString("due-in");
      const dueMs = dueIn ? parseDuration(dueIn) : null;

      if (dueIn && !dueMs) {
        await interaction.reply({
          content: "Use a valid due delay like `6h` or `3d`.",
          ephemeral: true
        });
        return;
      }

      const todo = {
        id: randomUUID().slice(0, 8),
        title: interaction.options.getString("title", true),
        assigneeId: interaction.options.getUser("assignee")?.id || null,
        createdBy: interaction.user.id,
        createdAt: Date.now(),
        dueAt: dueMs ? Date.now() + dueMs : null,
        done: false
      };

      guildData.todos.push(todo);
      saveStore();

      await interaction.reply({
        content: `Todo \`${todo.id}\` added.`,
        ephemeral: true
      });
      return;
    }

    const id = interaction.options.getString("id", true);
    const todo = guildData.todos.find((item) => item.id === id);

    if (!todo) {
      await interaction.reply({
        content: `Todo \`${id}\` does not exist.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "done") {
      todo.done = true;
      todo.doneAt = Date.now();
      saveStore();
      await interaction.reply({
        content: `Todo \`${id}\` marked done.`,
        ephemeral: true
      });
      return;
    }

    if (subcommand === "remove") {
      guildData.todos = guildData.todos.filter((item) => item.id !== id);
      saveStore();
      await interaction.reply({
        content: `Todo \`${id}\` removed.`,
        ephemeral: true
      });
    }
  }
};
