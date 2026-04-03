const fs = require("node:fs");
const path = require("node:path");

function loadCommands() {
  const commands = new Map();
  const commandsPath = path.join(__dirname, "..", "commands");
  const files = fs.readdirSync(commandsPath).filter((file) => file.endsWith(".js"));

  for (const file of files) {
    const command = require(path.join(commandsPath, file));
    commands.set(command.data.name, command);
  }

  return commands;
}

function loadCommandData() {
  return [...loadCommands().values()].map((command) => command.data.toJSON());
}

module.exports = {
  loadCommands,
  loadCommandData
};
